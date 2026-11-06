"""ORD-0503: the two member write doors, tested one refusal at a time.

The order asked for one counterexample per named refusal, and each one has to bite only its own
shape -- so almost every arm below carries its control: the same call with that one condition
removed, answering something else.  An arm that goes green both with and without the guard it names
is not testing the guard, which is how a "refusal" shipped in an earlier round of this project turned
out to be unreachable code.

What each arm is for:

1. `..._moves_a_seat_and_the_answer_is_the_row_read_back` -- the door writes, and what it hands back
   is `_member_payload` (the same helper `GET /api/v1/journeys/{journey_id}/members` uses at
   `backend/app/main.py`), not an object assembled from the request.  Also pins `joined_at`
   staying put while `updated_at` moves, and the audit row.
2. `..._unknown_member_row...` -- `MEMBER_UNKNOWN` (404).
3. `..._withdrawn_row...` -- `MEMBER_ALREADY_WITHDRAWN` (409) on a row whose removal already happened.
4. `..._change_that_changes_nothing...` -- `MEMBER_ROLE_UNCHANGED` (409), and the store proving no
   write happened.
5. `..._own_seat...` -- `MEMBER_ROLE_SELF_CHANGE_REFUSED` (409).
6. `..._owner_seat...` -- `MEMBER_OWNER_ROLE_IMMUTABLE` (409), in both directions.
7. `..._lacks_member_manage...` -- `MEMBER_MANAGE_NOT_PERMITTED` (403), with an OWNER control on the
   same target row.
8. `..._not_a_member_of_that_journey...` -- `MEMBER_ACTOR_NOT_IN_JOURNEY` (403).
9. `..._lapsed_membership...` -- `MEMBER_ACTOR_LAPSED` (403): a different answer from arm 8, for a
   person who really was invited and whose invitation ran out.
10. `..._admin_cap_refuses_the_third_promotion...` -- `MEMBER_ADMIN_CAP_REACHED` (409), naming the
    number.
11. `..._lapsed_admin_does_not_hold_a_seat...` -- the cap's denominator is in-force rows, measured.
12. `..._authority_is_asked_of_the_seeded_table...` -- every role in the store, and the answer must
    match `journey_role_capabilities` rather than a list typed into the door.
13-17. the self-exit door: it writes only the caller's row, an owner cannot walk out, a person with
    no row cannot exit somebody else's seat, leaving twice is not a new fact, and the withdrawn row
    stays listed.
18. `..._served_document_declares_every_refusal_the_contract_declares` -- ORD-0395's hole: a status
    the decorator does not name never reaches `/openapi.json`, and the parity door only compares
    path x method, so this arm reads both documents and the reason-code names in each.
19. `..._cap_is_spelled_the_same_in_all_three_places` -- the module constant, the contract's
    `x-citynexus-max-administrators-per-journey`, and the limits door's answer.
20. `..._shapes_are_not_reused` -- the self-exit body takes no `member_id`: this is the arm that goes
    red if somebody bolts the new semantics onto ORD-0434's withdraw door.

Nothing here calls `POST /api/v1/demo/reset`, and nothing touches the shared preview store: the
database is the one `backend/tests/conftest.py` resolves for this run.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, Journey, JourneyMember, JourneyRoleCapability, Traveler
from app.db.session import SessionLocal
from app.main import MAX_ADMINISTRATORS_PER_JOURNEY, app

client = TestClient(app, raise_server_exceptions=False)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
GENERATED = ROOT / "frontend/src/generated/api.ts"
MOMENT = datetime.now(timezone.utc).isoformat()

ROLE_PATH = "/api/v1/journeys/{journey}/members/{member}/role"
EXIT_PATH = "/api/v1/journeys/{journey}/members/self-exit"
LIMITS_PATH = "/api/v1/journeys/{journey}/member-role-limits"
ADDED = []


def _store_path() -> Path:
    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _reason(body: object) -> str | None:
    error = (body or {}).get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and isinstance(error.get("reason_code"), str):
        return error["reason_code"]
    return None


def _details(body: object) -> dict:
    error = (body or {}).get("error") if isinstance(body, dict) else None
    return error.get("details") if isinstance(error, dict) and isinstance(error.get("details"), dict) else {}


def _traveller(label: str) -> str:
    identifier = str(uuid4())
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=identifier, display_name=f"ORD-0503 探针 {label}", email=None, locale="zh-CN",
                timezone="Asia/Shanghai", created_at=MOMENT, updated_at=MOMENT,
            )
        )
        session.commit()
    return identifier


def _journey(traveler_id: str, label: str) -> str:
    identifier = str(uuid4())
    with SessionLocal() as session:
        session.add(
            Journey(
                id=identifier, traveler_id=traveler_id, title=f"沿海一周 {label}",
                city_code="CN-XIAMEN", timezone="Asia/Shanghai",
                start_at="2031-05-01T00:00:00+00:00", end_at="2031-05-03T00:00:00+00:00",
                budget_amount_minor=120000, budget_currency="CNY", status="PLANNED",
                created_at=MOMENT, updated_at=MOMENT,
            )
        )
        session.commit()
    return identifier


def _lapsed(days: int = -2) -> str:
    """An instant in the past, with the offset the door requires."""

    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _is_past(instant: str) -> bool:
    return datetime.fromisoformat(instant) <= datetime.now(timezone.utc)


def _add(journey_id: str, traveler_id: str, role: str, expires_at: str | None = None) -> dict:
    """Seat somebody through ORD-0434's own door rather than writing the row here.

    A fixture that inserted members directly would still pass if the door's rules drifted, and the
    cap arms below count on the rows the door actually made.
    """

    already_gone = expires_at is not None and _is_past(expires_at)
    answer = client.post(
        f"/api/v1/journeys/{journey_id}/members",
        json={"traveler_id": traveler_id, "role": role, "invited_by": "lane-1ba48dbf",
              "expires_at": None if already_gone else expires_at},
    )
    assert answer.status_code == 201, answer.text
    row = answer.json()
    if already_gone:
        # ORD-0461 r3 made this door refuse an invitation whose deadline had already gone by, because
        # the row it stored was read as lapsed in the same breath.  The seat is still made by the door;
        # the deadline is then moved into the past the way time moves it, so every arm below still
        # faces a lapsed membership and nothing about its assertions changed.
        with SessionLocal() as session:
            stored = session.get(JourneyMember, row["id"])
            assert stored is not None, row["id"]
            stored.expires_at = expires_at
            session.commit()
        # the state is taken back from the read door, not from what the write door answered before
        # the deadline was moved: the arms below ask what this membership is now.
        answer_rows = client.get(f"/api/v1/journeys/{journey_id}/members").json()
        row = [item for item in answer_rows if item["id"] == row["id"]][0]
    ADDED.append(row["id"])
    return row


def _stage(label: str) -> dict:
    """A journey with its owner and three seats ready to be moved: collaborator, admin, outsider.

    The outsider is a real traveller with no row here -- arm 8 needs "not invited" to be tellable
    apart from "invitation ran out", and only a second, different person can show that.
    """

    owner, collaborator, admin, outsider = (_traveller(f"{label}-{n}") for n in
                                            ("owner", "collab", "admin", "outsider"))
    journey_id = _journey(owner, label)
    rows = {
        "OWNER": _add(journey_id, owner, "OWNER"),
        "COLLABORATOR": _add(journey_id, collaborator, "COLLABORATOR"),
        "ADMIN": _add(journey_id, admin, "ADMIN"),
    }
    return {"journey": journey_id, "rows": rows, "outsider": outsider,
            "owner": owner, "collaborator": collaborator, "admin": admin}


def _role(journey_id: str, member_id: str, role: str, actor: str):
    return client.post(ROLE_PATH.format(journey=journey_id, member=member_id),
                       json={"role": role, "actor_traveler_id": actor})


def _leave(journey_id: str, traveler_id: str):
    return client.post(EXIT_PATH.format(journey=journey_id), json={"traveler_id": traveler_id})


def _listed(journey_id: str) -> dict[str, dict]:
    answer = client.get(f"/api/v1/journeys/{journey_id}/members")
    assert answer.status_code == 200, answer.text
    return {row["traveler_id"]: row for row in answer.json()}


def _row_of(traveler_id: str) -> dict:
    with SessionLocal() as session:
        member = session.scalars(
            select(JourneyMember).where(JourneyMember.traveler_id == traveler_id)
        ).first()
        assert member is not None, traveler_id
        return {"role": member.role, "joined_at": member.joined_at, "updated_at": member.updated_at,
                "removed_at": member.removed_at, "expires_at": member.expires_at}


def test_the_role_door_moves_a_seat_and_answers_the_row_it_read_back():
    stage = _stage("move")
    target = stage["rows"]["COLLABORATOR"]["id"]
    before = _row_of(stage["collaborator"])
    answer = _role(stage["journey"], target, "ADMIN", stage["owner"])
    assert answer.status_code == 200, answer.text
    moved = answer.json()
    assert moved["role"] == "ADMIN" and moved["id"] == target
    after = _row_of(stage["collaborator"])
    assert after["role"] == "ADMIN", after
    assert after["joined_at"] == before["joined_at"], "the seat moved, the day it was taken did not"
    assert after["updated_at"] != before["updated_at"], after
    assert _listed(stage["journey"])[stage["collaborator"]]["role"] == "ADMIN"
    assert _listed(stage["journey"])[stage["collaborator"]]["id"] == target
    with SessionLocal() as session:
        event = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == target,
                                   AuditLog.action == "member.role.changed")
        ).first()
    assert event is not None and json.loads(event.before_json)["role"] == "COLLABORATOR"
    assert json.loads(event.after_json)["role"] == "ADMIN"
    assert event.actor_type == "traveler" and event.actor_id == stage["owner"]


def test_a_row_that_is_not_in_this_journey_is_refused_by_name():
    stage = _stage("unknown")
    answer = _role(stage["journey"], "ord0503-no-such-member", "ADMIN", stage["owner"])
    assert answer.status_code == 404, answer.text
    assert _reason(answer.json()) == "MEMBER_UNKNOWN"
    assert _details(answer.json())["field"] == "member_id"
    # the control: the same call against a row that IS here gets past this guard
    other = _role(stage["journey"], stage["rows"]["COLLABORATOR"]["id"], "ADMIN", stage["owner"])
    assert _reason(other.json()) != "MEMBER_UNKNOWN", other.text


def test_a_withdrawn_seat_is_not_something_a_role_change_can_write():
    stage = _stage("withdrawn")
    row = stage["rows"]["COLLABORATOR"]
    assert client.post(
        f"/api/v1/journeys/{stage['journey']}/members/{row['id']}/withdraw").status_code == 200
    answer = _role(stage["journey"], row["id"], "ADMIN", stage["owner"])
    assert answer.status_code == 409, answer.text
    assert _reason(answer.json()) == "MEMBER_ALREADY_WITHDRAWN"
    assert _details(answer.json())["member_id"] == row["id"]
    assert _row_of(stage["collaborator"])["role"] == "COLLABORATOR", "the refused write stayed out"


def test_a_change_that_changes_nothing_is_refused_instead_of_touching_the_row():
    stage = _stage("noop")
    row = stage["rows"]["COLLABORATOR"]
    before = _row_of(stage["collaborator"])
    answer = _role(stage["journey"], row["id"], "COLLABORATOR", stage["owner"])
    assert answer.status_code == 409, answer.text
    assert _reason(answer.json()) == "MEMBER_ROLE_UNCHANGED"
    assert _details(answer.json())["role"] == "COLLABORATOR"
    assert _row_of(stage["collaborator"])["updated_at"] == before["updated_at"], \
        "a no-op that moves `updated_at` is a second account of the same seat"


def test_a_person_cannot_move_their_own_seat():
    stage = _stage("self")
    row = stage["rows"]["ADMIN"]
    answer = _role(stage["journey"], row["id"], "COLLABORATOR", stage["admin"])
    assert answer.status_code == 409, answer.text
    assert _reason(answer.json()) == "MEMBER_ROLE_SELF_CHANGE_REFUSED"
    assert _details(answer.json())["actor_traveler_id"] == stage["admin"]
    # control: the same move by the owner is a normal change, so the refusal is about who asked
    by_owner = _role(stage["journey"], row["id"], "COLLABORATOR", stage["owner"])
    assert by_owner.status_code == 200, by_owner.text


def test_the_owner_seat_does_not_move_through_this_door_in_either_direction():
    stage = _stage("owner-immutable")
    # The actor is another member with authority: naming the owner themself would answer
    # `MEMBER_ROLE_SELF_CHANGE_REFUSED` first, which is the documented order and its own arm.
    demotion = _role(stage["journey"], stage["rows"]["OWNER"]["id"], "ADMIN", stage["admin"])
    assert demotion.status_code == 409, demotion.text
    assert _reason(demotion.json()) == "MEMBER_OWNER_ROLE_IMMUTABLE"
    assert _details(demotion.json())["from_role"] == "OWNER"
    promotion = _role(stage["journey"], stage["rows"]["COLLABORATOR"]["id"], "OWNER", stage["owner"])
    assert promotion.status_code == 409, promotion.text
    assert _reason(promotion.json()) == "MEMBER_OWNER_ROLE_IMMUTABLE"
    assert _details(promotion.json())["to_role"] == "OWNER"
    owner_about_themself = _role(stage["journey"], stage["rows"]["OWNER"]["id"], "ADMIN",
                                stage["owner"])
    assert _reason(owner_about_themself.json()) == "MEMBER_ROLE_SELF_CHANGE_REFUSED", \
        owner_about_themself.text


def test_an_actor_whose_role_does_not_hold_member_manage_is_refused():
    stage = _stage("permission")
    answer = _role(stage["journey"], stage["rows"]["ADMIN"]["id"], "COLLABORATOR", stage["collaborator"])
    assert answer.status_code == 403, answer.text
    assert _reason(answer.json()) == "MEMBER_MANAGE_NOT_PERMITTED"
    details = _details(answer.json())
    assert details["actor_role"] == "COLLABORATOR"
    assert details["required_capability"] == "MEMBER_MANAGE"
    # control: same target, same new role, an actor whose role does hold the capability
    allowed = _role(stage["journey"], stage["rows"]["ADMIN"]["id"], "COLLABORATOR", stage["owner"])
    assert allowed.status_code == 200, allowed.text


def test_an_actor_with_no_row_here_and_an_actor_whose_row_ran_out_are_two_answers():
    stage = _stage("actors")
    nowhere = _role(stage["journey"], stage["rows"]["COLLABORATOR"]["id"], "ADMIN", stage["outsider"])
    assert nowhere.status_code == 403, nowhere.text
    assert _reason(nowhere.json()) == "MEMBER_ACTOR_NOT_IN_JOURNEY"
    lapsed_traveller = _traveller("actors-lapsed-actor")
    lapsed = _add(stage["journey"], lapsed_traveller, "OWNER", expires_at=_lapsed())
    ran_out = _role(stage["journey"], stage["rows"]["COLLABORATOR"]["id"], "ADMIN", lapsed_traveller)
    assert ran_out.status_code == 403, ran_out.text
    assert _reason(ran_out.json()) == "MEMBER_ACTOR_LAPSED"
    assert _details(ran_out.json())["actor_member_id"] == lapsed["id"]
    assert _details(nowhere.json()).get("actor_member_id") is None, \
        "the two refusals have to stay tellable apart from their own payloads"


def test_the_cap_refuses_the_third_admin_and_names_the_number_it_used():
    stage = _stage("cap")
    # The stage already holds one in-force ADMIN; fill up to the constant the door compares against
    # rather than assuming two, then try one seat past it.
    while _member_admins(stage["journey"]) < MAX_ADMINISTRATORS_PER_JOURNEY:
        _add(stage["journey"], _traveller("cap-filler"), "ADMIN")
    assert _member_admins(stage["journey"]) == MAX_ADMINISTRATORS_PER_JOURNEY
    candidate = _add(stage["journey"], _traveller("cap-candidate"), "COLLABORATOR")
    third = _role(stage["journey"], candidate["id"], "ADMIN", stage["owner"])
    assert third.status_code == 409, third.text
    assert _reason(third.json()) == "MEMBER_ADMIN_CAP_REACHED"
    details = _details(third.json())
    assert details["max_administrators"] == MAX_ADMINISTRATORS_PER_JOURNEY
    assert details["admin_count_in_force"] == MAX_ADMINISTRATORS_PER_JOURNEY
    assert _row_of(candidate["traveler_id"])["role"] == "COLLABORATOR", "the refused seat stayed put"
    # control: freeing one ADMIN seat makes the same promotion answer 200, so the cap is a count and
    # not a one-way door that only ever says no.
    first_admin = stage["rows"]["ADMIN"]
    demote = _role(stage["journey"], first_admin["id"], "COLLABORATOR", stage["owner"])
    assert demote.status_code == 200, demote.text
    assert _member_admins(stage["journey"]) == MAX_ADMINISTRATORS_PER_JOURNEY - 1
    assert _role(stage["journey"], candidate["id"], "ADMIN", stage["owner"]).status_code == 200


def test_a_lapsed_admin_does_not_hold_a_seat_against_the_cap():
    stage = _stage("cap-lapsed")
    running_out = _add(stage["journey"], _traveller("cap-lapsed-admin"), "ADMIN", expires_at=_lapsed())
    full = _add(stage["journey"], _traveller("cap-live-admin"), "ADMIN")
    assert _member_admins(stage["journey"]) == 2, _member_admins(stage["journey"])
    # The lapsed row is an ADMIN in the table and not one in force; only the second counts, so this
    # promotion is refused for having two live seats, not for having two rows.
    assert running_out["member_state"] == "MEMBER_LAPSED" and full["member_state"] == "MEMBER_IN_FORCE"
    answer = _role(stage["journey"], stage["rows"]["COLLABORATOR"]["id"], "ADMIN", stage["owner"])
    assert _reason(answer.json()) == "MEMBER_ADMIN_CAP_REACHED", answer.text
    assert _details(answer.json())["admin_count_in_force"] == MAX_ADMINISTRATORS_PER_JOURNEY


def _member_admins(journey_id: str) -> int:
    limits = client.get(LIMITS_PATH.format(journey=journey_id))
    assert limits.status_code == 200, limits.text
    return limits.json()["admin_count_in_force"]


def test_the_authority_question_is_asked_of_the_seeded_table_not_a_list_in_the_door():
    """For every role the store knows, the door's answer has to match `journey_role_capabilities`."""

    with SessionLocal() as session:
        rows = session.scalars(select(JourneyRoleCapability)).all()
    holds = {row.role for row in rows if row.capability == "MEMBER_MANAGE"}
    assert holds, "an empty ACL table would let this arm pass vacuously"
    stage = _stage("acl")
    for role in sorted({row.role for row in rows}):
        seat = _add(stage["journey"], _traveller(f"acl-{role}"), role)
        actor_with_role = seat["traveler_id"]
        target = _add(stage["journey"], _traveller(f"acl-target-{role}"), "COLLABORATOR")
        answer = _role(stage["journey"], target["id"], "ADMIN", actor_with_role)
        permitted = role in holds
        if permitted:
            # the actor names a seat that is not the target, so neither self-change nor owner rules
            # are what this call is answering to -- unless the role itself is OWNER, whose seat is
            # immutable and therefore never reaches the capability guard.
            assert _reason(answer.json()) != "MEMBER_MANAGE_NOT_PERMITTED", (role, answer.text)
        else:
            assert answer.status_code == 403, (role, answer.text)
            assert _reason(answer.json()) == "MEMBER_MANAGE_NOT_PERMITTED", (role, answer.text)


def test_a_member_can_take_themselves_out_and_the_row_stays_listed_as_withdrawn():
    stage = _stage("exit")
    answer = _leave(stage["journey"], stage["collaborator"])
    assert answer.status_code == 200, answer.text
    row = answer.json()
    assert row["member_state"] == "MEMBER_WITHDRAWN" and row["removed_at"]
    assert _listed(stage["journey"])[stage["collaborator"]]["member_state"] == "MEMBER_WITHDRAWN"
    assert _row_of(stage["collaborator"])["removed_at"] == row["removed_at"]


def test_the_self_exit_door_writes_the_callers_row_and_nobody_elses():
    stage = _stage("exit-one-row")
    before_admin = _row_of(stage["admin"])
    assert _leave(stage["journey"], stage["collaborator"]).status_code == 200
    assert _row_of(stage["admin"]) == before_admin, "a second row moved is a door with two targets"
    assert _row_of(stage["owner"])["removed_at"] is None


def test_the_owner_of_a_journey_cannot_walk_out_and_leave_it_unowned():
    stage = _stage("exit-owner")
    answer = _leave(stage["journey"], stage["owner"])
    assert answer.status_code == 409, answer.text
    assert _reason(answer.json()) == "MEMBER_OWNER_CANNOT_LEAVE"
    assert _details(answer.json())["member_id"] == stage["rows"]["OWNER"]["id"]
    assert _row_of(stage["owner"])["removed_at"] is None


def test_leaving_a_seat_you_do_not_hold_is_not_answered_as_a_withdrawal():
    stage = _stage("exit-outsider")
    answer = _leave(stage["journey"], stage["outsider"])
    assert answer.status_code == 404, answer.text
    assert _reason(answer.json()) == "MEMBER_SELF_EXIT_NOT_A_MEMBER"
    assert _details(answer.json())["field"] == "traveler_id"


def test_leaving_twice_is_not_a_new_fact():
    stage = _stage("exit-twice")
    assert _leave(stage["journey"], stage["collaborator"]).status_code == 200
    again = _leave(stage["journey"], stage["collaborator"])
    assert again.status_code == 409, again.text
    assert _reason(again.json()) == "MEMBER_ALREADY_WITHDRAWN"
    assert _details(again.json())["member_id"] == stage["rows"]["COLLABORATOR"]["id"]


def test_the_two_doors_never_delete_a_member_row():
    stage = _stage("no-delete")
    with SessionLocal() as session:
        before = len(session.scalars(
            select(JourneyMember).where(JourneyMember.journey_id == stage["journey"])).all())
    assert _leave(stage["journey"], stage["collaborator"]).status_code == 200
    assert _role(stage["journey"], stage["rows"]["ADMIN"]["id"], "COLLABORATOR",
                 stage["owner"]).status_code == 200
    with SessionLocal() as session:
        after = len(session.scalars(
            select(JourneyMember).where(JourneyMember.journey_id == stage["journey"])).all())
    assert after == before == 3, (before, after)


def _contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def test_the_served_document_declares_every_refusal_the_contract_declares():
    """ORD-0395's hole, armed for these three doors.

    The parity tool compares path x method, so a refusal named in the contract and missing from the
    decorator's `responses` would leave this lane green.  This arm reads the contract's status set
    and its `reason_code` words for each new path and requires the runtime document to carry the same
    statuses and name the same refusals.
    """

    spec = app.openapi()
    contract = _contract()["paths"]
    pairs = {
        "/journeys/{journey_id}/members/{member_id}/role": "post",
        "/journeys/{journey_id}/members/self-exit": "post",
        "/journeys/{journey_id}/member-role-limits": "get",
    }
    for path, verb in pairs.items():
        runtime_path = "/api/v1" + path
        declared = contract[path][verb]
        served = spec["paths"][runtime_path][verb]
        formal_codes = {c for c in declared["responses"] if c != 'description'}
        runtime_codes = set(served["responses"])
        assert formal_codes <= runtime_codes, (runtime_path, formal_codes, runtime_codes)
        names_in_contract = {
            word for status in formal_codes
            for word in str(declared["responses"][status].get("description", "")).split()
            if word.startswith("MEMBER_")
        }
        rendered = json.dumps(served["responses"], ensure_ascii=False)
        missing = {name for name in names_in_contract if name not in rendered}
        assert not missing, (runtime_path, sorted(missing))


def test_the_cap_is_spelled_the_same_in_all_three_places():
    limits = client.get(LIMITS_PATH.format(journey=_stage("cap-spelling")["journey"])).json()
    contract = _contract()["paths"]["/journeys/{journey_id}/members/{member_id}/role"]
    assert limits["max_administrators"] == MAX_ADMINISTRATORS_PER_JOURNEY
    assert contract["x-citynexus-max-administrators-per-journey"] == MAX_ADMINISTRATORS_PER_JOURNEY
    assert limits["roles"] == ["OWNER", "ADMIN", "COLLABORATOR"]


def test_the_new_bodies_reached_the_generated_client_and_take_no_foreign_fields():
    """The shapes are the order's promise: a self-exit body that grew a `member_id` is ORD-0434's door."""

    contract = _contract()["components"]["schemas"]
    assert contract["JourneyMemberSelfExit"]["properties"].keys() == {"traveler_id"}
    assert contract["JourneyMemberSelfExit"]["additionalProperties"] is False
    assert set(contract["JourneyMemberRoleChange"]["properties"]) == {"role", "actor_traveler_id"}
    assert set(contract["JourneyMemberRoleChange"]["required"]) == {"role", "actor_traveler_id"}
    text = GENERATED.read_text(encoding="utf-8")
    for component in ("JourneyMemberRoleChange", "JourneyMemberSelfExit", "MemberRoleLimits"):
        assert f"        {component}: {{" in text, component
    body_rejects = client.post(EXIT_PATH.format(journey=_stage("extra")["journey"]),
                               json={"traveler_id": str(uuid4()), "member_id": "whatever"})
    assert body_rejects.status_code == 422, body_rejects.text
    assert "extra_forbidden" in json.dumps(body_rejects.json()), body_rejects.text


def test_the_doors_answer_422_before_touching_a_store_when_the_body_is_wrong():
    stage = _stage("validation")
    not_a_uuid = _role(stage["journey"], stage["rows"]["COLLABORATOR"]["id"], "ADMIN", "not-a-uuid")
    assert not_a_uuid.status_code == 422, not_a_uuid.text
    unknown_role = _role(stage["journey"], stage["rows"]["COLLABORATOR"]["id"], "SUPERUSER",
                         stage["owner"])
    assert unknown_role.status_code == 422, unknown_role.text
    assert _row_of(stage["collaborator"])["role"] == "COLLABORATOR"


def test_a_store_level_unique_index_still_holds_after_the_new_doors_ran():
    """The cap arms write rows, so re-read the promise ORD-0434 made about one row per traveller."""

    stage = _stage("unique")
    with sqlite3.connect(_store_path()) as connection:
        indexes = list(connection.execute("PRAGMA index_list(journey_members)"))
        columns_of = {
            name: [r[2] for r in connection.execute(f"PRAGMA index_info('{name}')")]
            for (_seq, name, _u, _o, _p) in indexes
        }
    named = [name for name in columns_of if name.startswith("uq_journey_members")]
    assert named, sorted(columns_of)
    assert columns_of[named[0]] == ["journey_id", "traveler_id"], columns_of
    duplicate = client.post(
        f"/api/v1/journeys/{stage['journey']}/members",
        json={"traveler_id": stage["collaborator"], "role": "ADMIN", "invited_by": "lane-1ba48dbf"},
    )
    assert duplicate.status_code == 409, duplicate.text
    assert _reason(duplicate.json()) == "MEMBER_ALREADY_PRESENT"
