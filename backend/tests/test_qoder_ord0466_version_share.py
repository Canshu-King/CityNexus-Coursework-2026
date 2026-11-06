"""ORD-0466 - a share grant can be cut for one plan version, and the token reads that version.

Measured at the tip this slice was written against (`4efe7efb70ac4e08471da9bd5153f178348e4db9`): the
grant table carried `journey_id`, `token`, `visibility`, `created_by` and the two ending moments and
nothing else (`backend/app/db/models.py`), so "share this trip" was the finest thing the store
could say about sharing; `plan_versions` (`backend/app/db/models.py`) had no share exit at all.
The preset design says journeys *and versions* are shareable, so the gap is a scope column, not a new
table and not a second token door.

What each arm holds the code to:

* V1 a journey-level grant still answers *every* key the owner's own read answers with -- none may go
  missing -- the only keys it may answer beyond those are the ones `contracts/openapi.yaml` declares
  for this door, and every key that declaration marks required is actually answered.  ORD-0475 ruled
  that the first cut of this arm had the direction wrong: "no extra key" forbids every later feature,
  while the invariant ORD-0446 set is that what the server holds must not silently disappear or be
  replaced, and ORD-0481 asked for the comparison to run in both directions.  Additive, and an old row
  is a journey-level grant, not an unfinished choice;
* V2 a version-scoped grant reads that one version, and the plan it carries is the *same*
  `PlanVersion` shape `GET /journeys/{id}/plans` answers with - one shape, not a second one that can
  drift away from the owner's own view of the version;
* V3 the three ways a version id can be wrong are three named codes, and none of them mints a token
  (a grant born unopenable is the ORD-0460 defect repeating itself in another column);
* V4 the scope survives both read doors - the create answer and the list answer - because a grant
  whose scope only exists in the request that made it cannot be administered later;
* V5 when the version behind a live grant is gone, the token answers 409 with its own name rather
  than a 200 whose plan section is quietly absent;
* V6 the contract and the checked-in client both carry the new field and the new response keys, so
  the value is not something only the server knows about;
* V7 each of V1's two key arms is shown biting on a body doctored from a live read, because an arm
  that cannot go red is not a gate -- which is how V1 was first written.

Every code this file names is prefixed `SHARE_GRANT_`, the rule ORD-0434 set so the words do not
collide with the agent-approval status enum.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient
from pytest import raises

from app.db.models import PlanVersion, Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
GENERATED = ROOT / "frontend/src/generated/api.ts"

GRANT_PATH = "/api/v1/journeys/{journey_id}/share-grants"
TOKEN_PATH = "/api/v1/shared/journeys/{token}"
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 12, 3, 9, 0, tzinfo=TZ)


def _traveler() -> str:
    traveler_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="ORD-0466 probe",
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
            "title": "ORD-0466",
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


def _plan_version(journey_id: str, version_no: int = 1) -> str:
    """A version row, written the way the store holds it. No activity is needed: the grant is about
    which version a token opens, and an empty version is still a version."""
    plan_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            PlanVersion(
                id=plan_id,
                journey_id=journey_id,
                version_no=version_no,
                planning_mode="balanced",
                status="CANDIDATE",
                source="INITIAL",
                total_cost_minor=120000,
                currency="CNY",
                estimated_duration_min=480,
                created_at=now,
            )
        )
        session.commit()
    return plan_id


def _create(journey_id: str, plan_version_id: str | None = None) -> dict:
    body: dict = {"visibility": "LINK_READ", "created_by": "lane-newlane2"}
    if plan_version_id is not None:
        body["plan_version_id"] = plan_version_id
    response = client.post(GRANT_PATH.format(journey_id=journey_id), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _reason(payload: dict) -> str:
    return str((payload.get("error") or {}).get("reason_code"))


def _grant_count(journey_id: str) -> int:
    response = client.get(GRANT_PATH.format(journey_id=journey_id))
    assert response.status_code == 200, response.text
    return len(response.json())


def _share_door_declaration() -> tuple[set[str], set[str]]:
    """`(declared, promised)` for this door, both read out of `contracts/openapi.yaml`.

    `declared` is the whitelist: every key this door may answer with beyond the owner's own journey
    body.  `promised` is the same arm's `required` list: the keys a caller is told will always be
    there.  Read from the contract rather than restated here so there is one list to widen, and it is
    the list a reviewer reads.

    Deliberately *not* pinned to a set of names in this file: demanding the contract name exactly
    today's keys would rebuild the mistake ORD-0475 was opened to remove, one level up."""
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    arm = spec["paths"]["/shared/journeys/{token}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["allOf"][1]
    return set(arm["properties"]), set(arm.get("required", []))


def _key_drift(
    body: dict, owner_body: dict, declared: set[str], promised: set[str]
) -> tuple[set[str], set[str], set[str]]:
    """`(missing, unlisted, unanswered)` for one share body against the owner's read of the journey.

    `grant_scope` is the one key this slice added to every answer, so it is not counted as drift.
    A key the owner answers with and this body does not is `missing`; a key this body answers with,
    the owner does not, and the contract does not declare is `unlisted`; a key the contract marks
    required for this door and the body does not answer is `unanswered`.  The last one is what makes
    the choice ORD-0481 asked about a real one: taking `plan_state` back out of the payload without
    taking it out of the contract goes red here, not silently."""
    shared = set(body) - {"grant_scope"}
    return (
        set(owner_body) - shared,
        shared - set(owner_body) - declared,
        promised - set(body),
    )


def test_v1_journey_level_grant_keeps_its_shape_and_names_its_scope() -> None:
    journey_id = _journey()
    grant = _create(journey_id)
    assert grant["plan_version_id"] is None, "a grant with no version sent must not carry one"

    read = client.get(TOKEN_PATH.format(token=grant["token"]))
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["grant_scope"] == "JOURNEY", body.get("grant_scope")
    assert "shared_plan" not in body, "a journey-level grant opened a plan it was never cut for"
    # the journey half of the body is still the same shape the owner's own read answers with
    owner = client.get(f"/api/v1/journeys/{journey_id}")
    assert owner.status_code == 200, owner.text
    declared, promised = _share_door_declaration()
    missing, unlisted, unanswered = _key_drift(body, owner.json(), declared, promised)
    assert not missing, sorted(missing)
    assert not unlisted, sorted(unlisted)
    assert not unanswered, sorted(unanswered)


def test_v2_version_grant_reads_that_version_in_the_owners_own_shape() -> None:
    journey_id = _journey()
    plan_id = _plan_version(journey_id, version_no=3)
    grant = _create(journey_id, plan_id)
    assert grant["plan_version_id"] == plan_id

    read = client.get(TOKEN_PATH.format(token=grant["token"]))
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["grant_scope"] == "PLAN_VERSION"
    assert body["shared_plan"]["id"] == plan_id
    assert body["shared_plan"]["version_no"] == 3

    plans = client.get(f"/api/v1/journeys/{journey_id}/plans")
    assert plans.status_code == 200, plans.text
    owner_row = next(row for row in plans.json() if row["id"] == plan_id)
    assert set(body["shared_plan"]) == set(owner_row), sorted(
        set(body["shared_plan"]) ^ set(owner_row)
    )


def test_v3_three_ways_a_version_id_is_wrong_are_three_names_and_no_token() -> None:
    journey_id = _journey()
    other_journey = _journey()
    other_plan = _plan_version(other_journey)
    before = _grant_count(journey_id)

    malformed = client.post(
        GRANT_PATH.format(journey_id=journey_id),
        json={"visibility": "LINK_READ", "created_by": "x", "plan_version_id": "not-an-id"},
    )
    assert malformed.status_code == 422, malformed.text
    assert _reason(malformed.json()) == "SHARE_GRANT_VERSION_ID_UNREADABLE"

    unknown = client.post(
        GRANT_PATH.format(journey_id=journey_id),
        json={"visibility": "LINK_READ", "created_by": "x", "plan_version_id": str(uuid4())},
    )
    assert unknown.status_code == 422, unknown.text
    assert _reason(unknown.json()) == "SHARE_GRANT_VERSION_UNKNOWN"

    foreign = client.post(
        GRANT_PATH.format(journey_id=journey_id),
        json={"visibility": "LINK_READ", "created_by": "x", "plan_version_id": other_plan},
    )
    assert foreign.status_code == 409, foreign.text
    assert _reason(foreign.json()) == "SHARE_GRANT_VERSION_NOT_IN_JOURNEY"

    assert _grant_count(journey_id) == before, "a refused grant still minted a token"


def test_v4_scope_survives_the_list_door() -> None:
    journey_id = _journey()
    plan_id = _plan_version(journey_id)
    _create(journey_id, plan_id)
    listed = client.get(GRANT_PATH.format(journey_id=journey_id))
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert [row["plan_version_id"] for row in rows] == [plan_id]


def test_v5_a_grant_whose_version_is_gone_names_that_fact() -> None:
    journey_id = _journey()
    plan_id = _plan_version(journey_id)
    grant = _create(journey_id, plan_id)

    with SessionLocal() as session:
        row = session.get(PlanVersion, plan_id)
        session.delete(row)
        session.commit()

    read = client.get(TOKEN_PATH.format(token=grant["token"]))
    assert read.status_code == 409, read.text
    assert _reason(read.json()) == "SHARE_GRANT_VERSION_GONE"
    assert "shared_plan" not in read.json().get("error", {}), "the refusal leaked a plan body"


def test_v6_contract_and_checked_in_client_carry_the_scope() -> None:
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    create = spec["components"]["schemas"]["ShareGrantCreate"]["properties"]
    grant = spec["components"]["schemas"]["ShareGrant"]["properties"]
    assert "plan_version_id" in create, sorted(create)
    assert "plan_version_id" in grant, sorted(grant)
    assert "plan_version_id" in spec["components"]["schemas"]["ShareGrant"]["required"]

    shared = spec["paths"]["/shared/journeys/{token}"]["get"]["responses"]["200"]
    schema = shared["content"]["application/json"]["schema"]
    assert schema["allOf"][1]["properties"]["grant_scope"]["enum"] == ["JOURNEY", "PLAN_VERSION"]
    assert (
        schema["allOf"][1]["properties"]["shared_plan"]["$ref"]
        == "#/components/schemas/PlanVersion"
    )

    generated = GENERATED.read_text(encoding="utf-8")
    assert "grant_scope" in generated, "the client cannot see the scope the server answers with"
    assert generated.count("plan_version_id") >= 2, "one of the two schemas never reached the client"


def test_v7_every_key_arm_bites_on_a_doctored_body() -> None:
    """ORD-0475 asked for a counter-example per arm and ORD-0481 for both directions of the contract
    comparison, and this file is the evidence for why: V1 was first written as a set equality, which
    held quietly until ORD-0465 legitimately added two keys to this body and then named those two keys
    as the failure.  Each arm is fed a body doctored from a live read and must name the doctoring, the
    untouched body is fed last so an arm cannot be passing because its doctoring was a no-op, and one
    arm proves the old equality would have flagged the payload that is now lawful."""
    journey_id = _journey()
    grant = _create(journey_id)
    body = client.get(TOKEN_PATH.format(token=grant["token"])).json()
    owner = client.get(f"/api/v1/journeys/{journey_id}").json()
    declared, promised = _share_door_declaration()

    # Arm 1: one key the owner answers with is gone from the share body.
    dropped = {k: v for k, v in body.items() if k != "city_code"}
    assert "city_code" in owner and "city_code" in body, sorted(owner)
    assert len(dropped) == len(body) - 1, "removing city_code removed nothing"
    missing, unlisted, unanswered = _key_drift(dropped, owner, declared, promised)
    assert missing == {"city_code"}, sorted(missing)
    assert unlisted == set(), sorted(unlisted)
    assert unanswered == set(), sorted(unanswered)

    # Arm 2: one key the contract never declares is answered.
    invented = dict(body, guest_note="not in the contract")
    assert "guest_note" not in declared, sorted(declared)
    missing, unlisted, unanswered = _key_drift(invented, owner, declared, promised)
    assert missing == set(), sorted(missing)
    assert unlisted == {"guest_note"}, sorted(unlisted)
    assert unanswered == set(), sorted(unanswered)

    # Arm 3, the other direction: a key the contract marks required is no longer answered.  This is
    # the cell that makes ORD-0481's "收回" a decision instead of a silence -- retract the value from
    # the payload and leave the promise in the contract, and this names it.
    unpromised = {k: v for k, v in body.items() if k != "plan_state"}
    assert "plan_state" in promised and "plan_state" in body, sorted(promised)
    assert len(unpromised) == len(body) - 1, "removing plan_state removed nothing"
    missing, unlisted, unanswered = _key_drift(unpromised, owner, declared, promised)
    assert missing == set(), sorted(missing)
    assert unlisted == set(), sorted(unlisted)
    assert unanswered == {"plan_state"}, sorted(unanswered)

    # All three quiet on the real body, and the two keys ORD-0465 answers with are declared.
    missing, unlisted, unanswered = _key_drift(body, owner, declared, promised)
    assert (missing, unlisted, unanswered) == (set(), set(), set()), (
        sorted(missing),
        sorted(unlisted),
        sorted(unanswered),
    )
    assert {"plan_state", "active_plan"} <= declared, sorted(declared)

    # And the rule this order replaced, run against the same live pair, so the record shows what
    # refusing to change it would keep doing to every lawful payload.
    with raises(AssertionError):
        assert set(body) - {"grant_scope"} == set(owner)
