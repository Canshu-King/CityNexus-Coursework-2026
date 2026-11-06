"""ORD-0434 step 1 - a share grant becomes a server fact, and it ends on schedule.

Measured at the authoritative tip `3543928ea1b7a0466871b6fbc3c3d24f64b14044`: the share exit was a
client-side string concat (`frontend/src/lib/journeyLink.ts`), the journey read
(`backend/app/main.py`) accepted no caller identity at all, and nothing in the store could hold
a visibility, a deadline or a withdrawal. So "who can see this trip" existed only in a URL.

What the constraints became in code, and which arm proves each:

* the anonymous read is untouched - one arm compares that function's bytes against the same function
  recorded at the base tip in `review/ord0434/ANON_READ_BASELINE_r1.json`, and another arm shows the
  behaviour: a journey whose grant has been revoked is still readable anonymously, because tightening
  that door is step 2 and is decided with the user, not shipped under it;
* expiry and withdrawal are two different named codes, and "never existed" is a third - the arms read
  all three off the same request shape, so a client cannot collapse them into one message;
* revocation is immediate and not repeatable: the same POST gives a different answer the second time;
* `PRIVATE` and `INVITEES_ONLY` are stored but fail closed on the token read, because there is no
  member data yet to check a holder against;
* no deadline is stored as a date: a grant without `expires_at` answers `null`, and an expiry sent
  without an offset is refused rather than silently read in the server's own zone.

The vocabulary was checked before naming anything: `PRIVATE`/`LINK_READ`/`INVITEES_ONLY`/`PUBLIC`
have no other meaning in this tree, while `EXPIRED`/`REVOKED` already belong to the agent-approval
status enum (`contracts/openapi.yaml`), so this slice never emits those bare words - every code
it adds is prefixed `SHARE_GRANT_`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient

from app.db.models import JourneyMember, JourneyShareGrant, Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
GENERATED = ROOT / "frontend/src/generated/api.ts"
BASELINE_ARTIFACT = ROOT / "review/ord0434/ANON_READ_BASELINE_r1.json"
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)

GRANT_PATH = "/api/v1/journeys/{journey_id}/share-grants"
REVOKE_PATH = "/api/v1/journeys/{journey_id}/share-grants/{grant_id}/revoke"
TOKEN_PATH = "/api/v1/shared/journeys/{token}"


def _traveler() -> str:
    traveler_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="ORD-0434 probe",
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
            "title": "ORD-0434",
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


def _create(journey_id: str, visibility: str = "LINK_READ", expires_at: str | None = None) -> dict:
    response = client.post(
        GRANT_PATH.format(journey_id=journey_id),
        json={"visibility": visibility, "created_by": "lane-rel", "expires_at": expires_at},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _reason(payload: dict) -> str:
    """The named fact rides in `error.reason_code`; `error.code` stays the one existing family."""
    return str((payload.get("error") or {}).get("reason_code"))


# ---------------------------------------------------------------- grant as a server fact


def test_grant_creation_answers_a_token_and_stores_no_invented_deadline() -> None:
    journey_id = _journey()
    grant = _create(journey_id)
    assert grant["visibility"] == "LINK_READ"
    assert len(grant["token"]) >= 20, "the token is the only capability the holder gets"
    # "no deadline" is NULL, not a far-future date and not the server's clock.
    assert grant["expires_at"] is None
    assert grant["revoked_at"] is None
    assert grant["created_by"] == "lane-rel"
    assert grant["journey_id"] == journey_id


def test_one_journey_holds_many_grants_and_the_list_keeps_withdrawn_rows() -> None:
    journey_id = _journey()
    first = _create(journey_id, "LINK_READ")
    second = _create(journey_id, "PUBLIC")
    listed = client.get(GRANT_PATH.format(journey_id=journey_id))
    assert listed.status_code == 200
    ids = [row["id"] for row in listed.json()]
    assert sorted(ids) == sorted([first["id"], second["id"]])

    revoked = client.post(REVOKE_PATH.format(journey_id=journey_id, grant_id=first["id"]))
    assert revoked.status_code == 200, revoked.text
    after = client.get(GRANT_PATH.format(journey_id=journey_id))
    rows = {row["id"]: row for row in after.json()}
    # A row that vanished would look identical to one that never existed; the bin stays visible.
    assert first["id"] in rows, "a withdrawn grant must still be listed, marked as withdrawn"
    assert rows[first["id"]]["revoked_at"], rows[first["id"]]
    assert rows[second["id"]]["revoked_at"] is None


def test_an_expiry_without_an_offset_is_refused_rather_than_guessed() -> None:
    journey_id = _journey()
    naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None).isoformat()
    response = client.post(
        GRANT_PATH.format(journey_id=journey_id),
        json={"visibility": "LINK_READ", "created_by": "lane-rel", "expires_at": naive},
    )
    assert response.status_code == 422, response.text
    assert _reason(response.json()) == "SHARE_GRANT_EXPIRY_NO_OFFSET"


# ---------------------------------------------------------------- the two-way readings


def test_revoke_then_the_same_request_gives_a_different_answer() -> None:
    journey_id = _journey()
    grant = _create(journey_id)
    same_request = REVOKE_PATH.format(journey_id=journey_id, grant_id=grant["id"])
    first = client.post(same_request)
    assert first.status_code == 200, first.text
    assert first.json()["already_revoked"] is False
    second = client.post(same_request)
    assert second.status_code != first.status_code, "the same request must not answer the same twice"
    assert _reason(second.json()) == "SHARE_GRANT_ALREADY_REVOKED"


def test_a_revoked_token_stops_working_on_the_next_read() -> None:
    journey_id = _journey()
    grant = _create(journey_id)
    before = client.get(TOKEN_PATH.format(token=grant["token"]))
    assert before.status_code == 200, before.text
    assert before.json()["id"] == journey_id

    assert client.post(REVOKE_PATH.format(journey_id=journey_id, grant_id=grant["id"])).status_code == 200
    after = client.get(TOKEN_PATH.format(token=grant["token"]))
    assert after.status_code == 409, after.text
    assert _reason(after.json()) == "SHARE_GRANT_REVOKED"
    assert after.json()["error"]["details"]["grant_id"] == grant["id"]


def _store_aged_grant(journey_id: str, visibility: str, expires_at: str) -> dict:
    """Put a grant row in the store whose deadline has already gone by.

    ORD-0460 made the create door refuse a past `expires_at`, so this arm can no longer mint one that
    way -- and it should not: the state a traveller actually meets is a grant that was created in the
    future and *aged*.  Writing the row here is that state, without waiting for the clock.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    grant = JourneyShareGrant(
        id=str(uuid4()),
        journey_id=journey_id,
        token="aged-" + uuid4().hex[:20],
        visibility=visibility,
        created_by="ord0434-probe",
        expires_at=expires_at,
        revoked_at=None,
        created_at=stamp,
        updated_at=stamp,
    )
    with SessionLocal() as session:
        session.add(grant)
        session.commit()
    return {"token": grant.token, "expires_at": expires_at}


def test_expired_and_never_existed_are_different_named_codes() -> None:
    journey_id = _journey()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    grant = _store_aged_grant(journey_id, "LINK_READ", past)
    expired = client.get(TOKEN_PATH.format(token=grant["token"]))
    assert expired.status_code == 409, expired.text
    assert _reason(expired.json()) == "SHARE_GRANT_EXPIRED"
    assert expired.json()["error"]["details"]["expires_at"] == grant["expires_at"]

    absent = client.get(TOKEN_PATH.format(token="not-a-grant-" + uuid4().hex))
    assert _reason(absent.json()) == "SHARE_GRANT_UNKNOWN"
    assert absent.status_code == 404, "an unknown token is not the same fact as an expired one"
    assert _reason(absent.json()) != _reason(expired.json())


def test_private_stays_closed_and_invitees_are_asked_for_a_name() -> None:
    """Step 1 refused both; step 2 has a member set, so the two档 now answer differently."""
    journey_id = _journey()
    private = _create(journey_id, "PRIVATE")
    refused = client.get(TOKEN_PATH.format(token=private["token"]))
    assert refused.status_code == 409, refused.text
    assert _reason(refused.json()) == "SHARE_GRANT_NOT_LINK_READABLE"

    invitees = _create(journey_id, "INVITEES_ONLY")
    unnamed = client.get(TOKEN_PATH.format(token=invitees["token"]))
    assert unnamed.status_code == 409, unnamed.text
    assert _reason(unnamed.json()) == "SHARE_GRANT_MEMBERSHIP_REQUIRED"

    stranger = _traveler()
    wrong = client.get(TOKEN_PATH.format(token=invitees["token"]), params={"member": stranger})
    assert _reason(wrong.json()) == "SHARE_GRANT_NOT_A_MEMBER", wrong.text
    assert _reason(wrong.json()) != _reason(unnamed.json()), (
        "nobody named and a name that does not hold are two different facts"
    )


def test_a_public_grant_reads_the_journey() -> None:
    journey_id = _journey()
    grant = _create(journey_id, "PUBLIC")
    response = client.get(TOKEN_PATH.format(token=grant["token"]))
    assert response.status_code == 200, response.text
    assert response.json()["id"] == journey_id


# ---------------------------------------------------------------- the untouched door


def _function_slice(text: str) -> str:
    start = text.index('@app.get("/api/v1/journeys/{journey_id}")')
    end = text.index('@app.get("/api/v1/journeys/{journey_id}/experience")')
    return text[start:end]


def test_the_anonymous_read_bytes_are_unchanged_from_the_base_tip() -> None:
    baseline = json.loads(BASELINE_ARTIFACT.read_text(encoding="utf-8"))
    live = _function_slice((ROOT / "backend/app/main.py").read_text(encoding="utf-8"))
    # The region now also holds the new grant doors, so the claim is about the function's own bytes:
    # the base tip's text must still appear verbatim inside it.
    assert baseline["get_journey_text"] in live, (
        "ORD-0434 promised the anonymous read would not move one word; it moved"
    )


def test_a_revoked_grant_does_not_change_what_the_anonymous_read_answers() -> None:
    journey_id = _journey()
    grant = _create(journey_id)
    before = client.get(f"/api/v1/journeys/{journey_id}")
    assert client.post(REVOKE_PATH.format(journey_id=journey_id, grant_id=grant["id"])).status_code == 200
    after = client.get(f"/api/v1/journeys/{journey_id}")
    assert before.status_code == after.status_code == 200
    assert before.json() == after.json(), (
        "the phone demo chain and every existing e2e walk this door; grants must not reach it"
    )


# ---------------------------------------------------------------- contract and client, one commit


def test_contract_and_generated_client_carry_the_same_four_doors() -> None:
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    # The hand-written contract keys carry no `/api/v1` prefix; the servers block supplies it.
    declared = {
        "/journeys/{journey_id}/share-grants",
        "/journeys/{journey_id}/share-grants/{grant_id}/revoke",
        "/shared/journeys/{token}",
    }
    missing = sorted(path for path in declared if path not in spec["paths"])
    assert not missing, f"declared in code but absent from the contract: {missing}"

    operations = {
        operation["operationId"]
        for item in spec["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and operation.get("operationId", "").startswith(("createShare", "listShare", "revokeShare", "readShared"))
    }
    assert operations == {
        "createShareGrant",
        "listShareGrants",
        "revokeShareGrant",
        "readSharedJourney",
    }, operations
    generated = GENERATED.read_text(encoding="utf-8")
    unported = sorted(op for op in operations if op not in generated)
    assert not unported, f"the checked-in client is behind the contract: {unported}"

    visibility = spec["components"]["schemas"]["ShareVisibility"]["enum"]
    assert visibility == ["PRIVATE", "LINK_READ", "INVITEES_ONLY", "PUBLIC"]


# ------------------------------------------------------------ step 2: members, ACL, scoped bin

MEMBER_PATH = "/api/v1/journeys/{journey_id}/members"
WITHDRAW_PATH = "/api/v1/journeys/{journey_id}/members/{member_id}/withdraw"
ACL_PATH = "/api/v1/journeys/{journey_id}/acl/roles"
BIN_PATH = "/api/v1/recycle-bin/journeys"
OWNER_BIN_PATH = "/api/v1/travelers/{traveler_id}/recycle-bin/journeys"


def _add_member(journey_id: str, traveler_id: str, role: str = "COLLABORATOR",
                expires_at: str | None = None) -> dict:
    already_gone = expires_at is not None and datetime.fromisoformat(expires_at)         <= datetime.now(timezone.utc)
    response = client.post(
        MEMBER_PATH.format(journey_id=journey_id),
        json={"traveler_id": traveler_id, "role": role, "invited_by": "lane-rel",
              "expires_at": None if already_gone else expires_at},
    )
    assert response.status_code == 201, response.text
    row = response.json()
    if already_gone:
        # ORD-0461 r3: this door no longer stores a membership born lapsed, so the deadline is moved
        # after the seat is made.  The arm below still reads an out-of-date membership.
        with SessionLocal() as session:
            stored = session.get(JourneyMember, row["id"])
            assert stored is not None, row["id"]
            stored.expires_at = expires_at
            session.commit()
        # read the state back through the list door: the arm asks what this membership is now, and the
        # 201 it was seated with predates the moved deadline.
        answer_rows = client.get(MEMBER_PATH.format(journey_id=journey_id)).json()
        row = [item for item in answer_rows if item["id"] == row["id"]][0]
    return row


def test_the_member_set_is_one_row_per_traveller_and_says_so_by_name() -> None:
    journey_id = _journey()
    member = _add_member(journey_id, _traveler())
    assert member["member_state"] == "MEMBER_IN_FORCE"
    assert member["expires_at"] is None, "no deadline was given, so none is stored"

    again = client.post(
        MEMBER_PATH.format(journey_id=journey_id),
        json={"traveler_id": member["traveler_id"], "role": "ADMIN", "invited_by": "lane-rel"},
    )
    assert again.status_code == 409, again.text
    assert _reason(again.json()) == "MEMBER_ALREADY_PRESENT"
    listed = client.get(MEMBER_PATH.format(journey_id=journey_id)).json()
    assert len(listed) == 1, "a refused second invitation must not add a second row"


def test_withdrawal_is_immediate_and_not_repeatable() -> None:
    journey_id = _journey()
    traveler_id = _traveler()
    member = _add_member(journey_id, traveler_id)
    grant = _create(journey_id, "INVITEES_ONLY")
    token_url = TOKEN_PATH.format(token=grant["token"])

    before = client.get(token_url, params={"member": traveler_id})
    assert before.status_code == 200, before.text
    assert before.json()["id"] == journey_id

    first = client.post(WITHDRAW_PATH.format(journey_id=journey_id, member_id=member["id"]))
    assert first.status_code == 200, first.text
    assert first.json()["member_state"] == "MEMBER_WITHDRAWN"
    after = client.get(token_url, params={"member": traveler_id})
    assert after.status_code == 409 and _reason(after.json()) == "SHARE_GRANT_NOT_A_MEMBER", (
        "the withdrawal has to bite on the next read, not on the next restart"
    )

    twice = client.post(WITHDRAW_PATH.format(journey_id=journey_id, member_id=member["id"]))
    assert twice.status_code != first.status_code, "the same withdrawal twice is not the same answer twice"
    assert _reason(twice.json()) == "MEMBER_ALREADY_WITHDRAWN"
    listed = client.get(MEMBER_PATH.format(journey_id=journey_id)).json()
    assert [row["id"] for row in listed] == [member["id"]], "the withdrawn row stays in the set, marked"
    assert listed[0]["removed_at"], listed[0]


def test_an_in_force_member_reads_an_invitees_only_grant() -> None:
    journey_id = _journey()
    traveler_id = _traveler()
    _add_member(journey_id, traveler_id)
    grant = _create(journey_id, "INVITEES_ONLY")
    ok = client.get(TOKEN_PATH.format(token=grant["token"]), params={"member": traveler_id})
    assert ok.status_code == 200, ok.text
    assert ok.json()["id"] == journey_id

    lapsed = _add_member(journey_id, _traveler(), "COLLABORATOR",
                         expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
    assert lapsed["member_state"] == "MEMBER_LAPSED"
    assert _reason(client.get(TOKEN_PATH.format(token=grant["token"]),
                              params={"member": lapsed["traveler_id"]}).json()) == (
        "SHARE_GRANT_NOT_A_MEMBER"
    ), "a lapsed membership must not read"


def test_the_acl_table_answers_the_product_own_question_about_a_role() -> None:
    journey_id = _journey()
    rows = client.get(ACL_PATH.format(journey_id=journey_id)).json()
    by_role = {row["role"]: row for row in rows}
    assert sorted(by_role) == ["ADMIN", "COLLABORATOR", "OWNER"], by_role
    assert "MEMBER_MANAGE" in by_role["ADMIN"]["capabilities"]
    assert "MEMBER_MANAGE" not in by_role["COLLABORATOR"]["capabilities"]
    assert set(by_role["OWNER"]["capabilities"]) == {
        "JOURNEY_READ", "JOURNEY_UPDATE", "PLACE_EDIT", "MEMBER_MANAGE", "SHARE_GRANT_MANAGE",
    }
    for row in rows:
        assert row["scope_message_key"].startswith("member.scope."), (
            "the table holds a key, not a sentence a screen would have to translate back"
        )


def test_the_bin_scopes_to_an_owner_and_answers_the_same_without_one() -> None:
    owner = _traveler()
    other = _traveler()
    mine = client.post("/api/v1/journeys", json={
        "traveler_id": owner, "title": "ORD-0434 mine", "city_code": "CN-XIAMEN",
        "timezone": "Asia/Shanghai", "start_at": START.isoformat(),
        "end_at": (START + timedelta(days=1)).isoformat(),
        "budget": {"amount_minor": 1000, "currency": "CNY"}, "planning_mode": "balanced"}).json()["id"]
    theirs = client.post("/api/v1/journeys", json={
        "traveler_id": other, "title": "ORD-0434 theirs", "city_code": "CN-XIAMEN",
        "timezone": "Asia/Shanghai", "start_at": START.isoformat(),
        "end_at": (START + timedelta(days=1)).isoformat(),
        "budget": {"amount_minor": 1000, "currency": "CNY"}, "planning_mode": "balanced"}).json()["id"]
    for journey_id in (mine, theirs):
        assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204

    whole = {row["id"] for row in client.get(BIN_PATH).json()}
    assert {mine, theirs} <= whole, "the whole-bin door still answers the whole bin"
    scoped = {row["id"] for row in client.get(OWNER_BIN_PATH.format(traveler_id=owner)).json()}
    assert scoped == {mine}, scoped
    assert client.get(OWNER_BIN_PATH.format(traveler_id=uuid4())).json() == []
    for row in client.get(OWNER_BIN_PATH.format(traveler_id=owner)).json():
        assert row["bin_state"] == "IN_RECYCLE_BIN" and row["deleted_at"], row


def test_the_whole_bin_door_still_advertises_no_filter() -> None:
    """ORD-0434 tried to add a query parameter here and an ORD-0402 arm said no; that promise holds.

    The scoped view is a door of its own, so this one keeps the constraint it was cut with: no
    pagination, no scope, no filter parameter in the contract or at runtime.
    """
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    door = spec["paths"]["/recycle-bin/journeys"]
    assert "parameters" not in door and "parameters" not in door["get"]
    runtime = client.get("/openapi.json").json()["paths"]["/api/v1/recycle-bin/journeys"]["get"]
    assert runtime.get("parameters", []) == [], runtime.get("parameters")
